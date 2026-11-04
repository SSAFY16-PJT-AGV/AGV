
#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "AGVGrabbableObject.h"
#include "AGVDisplayShelf.generated.h"

class USceneComponent;
class UStaticMeshComponent;

UCLASS()
class AGVSIMULATION_API AAGVDisplayShelf : public AActor
{
    GENERATED_BODY()

public:
    AAGVDisplayShelf();

protected:
    // 진열대 중심
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Shelf")
    USceneComponent* ShelfRoot;

    // 진열대 외형
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Shelf")
    UStaticMeshComponent* BottomBoard;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Shelf")
    UStaticMeshComponent* MiddleBoard;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Shelf")
    UStaticMeshComponent* LeftPost;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Shelf")
    UStaticMeshComponent* RightPost;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Shelf")
    UStaticMeshComponent* BackPanel;

    // 상품을 배치할 6개 슬롯
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Shelf")
    TArray<TObjectPtr<USceneComponent>> Slots;

    // 진열대가 취급하는 상품 종류
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Shelf")
    EProductType ShelfProductType = EProductType::Red;


    // 초기 상품이 배치될 슬롯 번호
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Shelf|Initial")
    TArray<int32> InitialOccupiedSlots;

    // 초기 상품으로 생성할 블루프린트 클래스
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Shelf|Initial")
    TSubclassOf<AAGVGrabbableObject> InitialProductClass;

    // 슬롯별 현재 상품
    UPROPERTY(VisibleInstanceOnly, BlueprintReadOnly, Category = "Shelf|Slots")
    TArray<TObjectPtr<AAGVGrabbableObject>> OccupiedSlots;

    virtual void BeginPlay() override;


public:
    UFUNCTION(BlueprintPure, Category = "Shelf")
    EProductType GetShelfProductType() const
    {
        return ShelfProductType;
    }

    UFUNCTION(BlueprintPure, Category = "Shelf")
    FVector GetSlotWorldLocation(int32 SlotIndex) const;

    UFUNCTION(BlueprintPure, Category = "Shelf")
    bool IsSlotOccupied(int32 SlotIndex) const;

    // 상품이 어느 슬롯에 있는지 검색
    int32 FindProductSlot(
        const AAGVGrabbableObject* Product
    ) const;

    // 상품을 집었을 때 슬롯 비우기
    bool RemoveProductFromSlot(
        AAGVGrabbableObject* Product
    );

    // 조준 위치에서 가장 가까운 슬롯 검색
    int32 FindNearestSlot(const FVector& WorldLocation) const;

    // 지정한 슬롯에 상품 진열
    bool PlaceProductAtSlot(
        AAGVGrabbableObject* Product,
        int32 SlotIndex
    );
};
